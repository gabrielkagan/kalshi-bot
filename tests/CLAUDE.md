# Tests

3,368 tests. Pytest. `conftest.py` at repo root.

## Run
- All: `python3 -m pytest tests/ -x`
- One file: `python3 -m pytest tests/test_<name>.py -x`
- One test: `python3 -m pytest tests/test_<name>.py::test_func -x`

## Conventions
- One test file per concern. Mirror the bot.py class/function being tested.
- Real DB, not mocks — integration tests must hit a real sqlite3 file (use `tmp_path`).
- For sizing/Kelly assertions, use the actual `OrderExecutor` paths, never reimplement Kelly inline.
- Regression tests after bug fixes: name `test_<bug_keyword>_regression` and reference the commit/incident in a one-line docstring.

## When adding a test
- Match existing file naming and fixture patterns — read 2-3 sibling tests first.
- AST-style guards (`test_call_sites.py`, `test_db_signatures.py`, `test_config_consistency.py`) catch signature drift; extend these rather than writing parallel checks when the failure mode fits.
