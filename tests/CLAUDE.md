# Tests

3,744 tests collected (post-Bit-6.1). Pytest. `conftest.py` at repo root.
Verify exact count with `python3 -m pytest tests/ --collect-only -q | tail -1`;
this header drifts as bits ship and is only refreshed when an extraction touches
`tests/CLAUDE.md` directly.

## Run
- All: `python3 -m pytest tests/ -x`
- One file: `python3 -m pytest tests/test_<name>.py -x`
- One test: `python3 -m pytest tests/test_<name>.py::test_func -x`

## Conventions
- One test file per concern. Mirror the bot/_impl.py class/function being tested.
- Real DB, not mocks — integration tests must hit a real sqlite3 file (use `tmp_path`).
- For sizing/Kelly assertions, use the actual `OrderExecutor` paths, never reimplement Kelly inline.
- Regression tests after bug fixes: name `test_<bug_keyword>_regression` and reference the commit/incident in a one-line docstring.

## When adding a test
- Match existing file naming and fixture patterns — read 2-3 sibling tests first.
- AST-style guards (`test_call_sites.py`, `test_db_signatures.py`, `test_config_consistency.py`) catch signature drift; extend these rather than writing parallel checks when the failure mode fits.

## Equivalence harness (`tests/equivalence/`, Pillar 3)

- Snapshot files (`tests/equivalence/test_*/`) pin engine outputs.
  **Do not** run `pytest --force-regen` autonomously — regen is a
  human-with-diff-review operation. If a snapshot fails, investigate
  the divergence first; the snapshot is the contract.
- `conftest.py::isolate_calibration_singletons` patches
  `_CALIBRATION_ENGINE` to None so `ProbabilityEngine.compute()` takes
  the deterministic passthrough/fixed-beta cascade. Bit 6.3 (CalibrationEngine
  extraction) owns extending this to inject a frozen learned-method oracle.
- Full runbook: `tests/equivalence/REGEN.md`.
