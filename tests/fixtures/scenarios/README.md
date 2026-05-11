# Test Fixture Scenarios

Sprint 13 Bit 13.4 (2026-05-11). Reproducible `state.db` scenarios
for tests that need a populated DB beyond what `tmp_path` + empty
schema provides.

## Convention

Each scenario is a `.sql` file in this directory:
- Header comment: scenario name, intent, what it populates.
- DDL via `CREATE TABLE` (matches production schema — see
  `agent_docs/db_schema.md`).
- INSERT statements populating representative rows.
- No bot-runtime dependencies — pure SQL, loadable into any sqlite3
  DB.

Tests that consume a scenario:
1. `import sqlite3` + `tmp_path / "state.db"`.
2. `conn.executescript(open(SCENARIO_PATH).read())`.
3. Run the test's assertions against the populated DB.

## Existing scenarios

| Scenario | Intent | Used by |
|---|---|---|
| `typical-15m-trade.sql` | One complete 15M trade lifecycle: candidate eval → maker_pending order → settled_trade with positive PnL. Includes the related evaluated_opportunity + market_observations_continuous baseline. | (any test needing a "happy path" 15M sample row set; consume via `executescript`) |

## Why .sql instead of .json or .parquet

- **Schema-aware**: DDL lives alongside the data, so a scenario is
  self-contained even if the production schema drifts (test loads its
  own DDL, doesn't rely on bot's schema at import time).
- **Diff-friendly**: text-based, line-oriented, easy to review in PRs.
- **Sibling parity**: `engine_inputs.parquet` exists for engine-test
  numeric snapshots (Pillar 3 frozen calibration). `.sql` scenarios are
  for higher-level DB state.

## Adding a new scenario

1. Copy `.claude/templates/new-audit.md` for inspiration on conventions
   (regime filter, Wilson CI, etc. — these are downstream of fixture
   data, but the scaffold helps).
2. Create `tests/fixtures/scenarios/<scenario-name>.sql` with:
   - Header comment block (purpose, intent, used-by).
   - CREATE TABLE statements for the tables you populate.
   - INSERT statements with representative values.
3. Document in this README (add a row to "Existing scenarios").
4. Pin in `tests/test_makefile.py` or similar AST guard so the scenario
   file isn't silently deleted.

## Caveats

- Don't put production secrets (API keys, real account balances) in a
  scenario. Use synthetic data.
- Don't mirror the entire production schema unless the test needs it —
  keep DDL focused on the tables the test actually reads.
- Regime-filter logic depends on `git log` history; scenarios CAN'T
  reproduce that. Tests that need regime-filter checks should mock
  `_detect_regime_cutoff()` separately.
